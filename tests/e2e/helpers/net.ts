// Network-condition helpers. setOffline + blockHost are the workhorses for
// "what happens when realtime drops?" / "dexie-cloud SaaS unavailable" specs.
import type { BrowserContext } from '@playwright/test'

export async function setOffline(
  context: BrowserContext,
  offline: boolean
): Promise<void> {
  await context.setOffline(offline)
}

// Refuse new ws/wss connections at the route layer. Playwright's `route` only
// covers http(s); for ws we need the explicit URL filter.
export async function blockWS(context: BrowserContext): Promise<void> {
  await context.route('**/*', async (route, req) => {
    const url = req.url()
    if (url.startsWith('ws://') || url.startsWith('wss://')) {
      await route.abort('failed')
      return
    }
    await route.continue()
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
