// IndexedDB / Dexie inspection helpers. Speak to window.__db__ exposed by
// src/boot/expose-debug.ts (gated by EXPOSE_DB=true at build time).
//
// Why page.evaluate over a Node-side IDB shim: Dexie's schema, hooks and
// dexie-cloud addon configuration all live in the page context. Replicating
// them outside the page would drift; reading through the same db instance
// the app uses is authoritative.
import type { Page } from '@playwright/test'

const READY_TIMEOUT_MS = 15_000

export async function exposeReady(page: Page): Promise<void> {
  await page.waitForFunction(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => !!(window as any).__exposeDebugReady__,
    undefined,
    { timeout: READY_TIMEOUT_MS }
  )
}

export async function dumpTable<T = unknown>(
  page: Page,
  table: string
): Promise<T[]> {
  await exposeReady(page)
  return page.evaluate(async (t) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const db = (window as any).__db__
    return await db[t].toArray()
  }, table)
}

export async function maxVersion(page: Page, table: string): Promise<number> {
  const rows = await dumpTable<{ version?: number }>(page, table)
  return rows.reduce((acc, r) => Math.max(acc, r.version ?? 0), 0)
}

export async function clearAll(
  page: Page,
  tables: string[]
): Promise<void> {
  await exposeReady(page)
  await page.evaluate(async (ts) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const db = (window as any).__db__
    for (const t of ts) await db[t].clear()
  }, tables)
}

export async function putRow(
  page: Page,
  table: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  row: any
): Promise<void> {
  await exposeReady(page)
  await page.evaluate(
    async ({ t, r }) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await (window as any).__db__[t].put(r)
    },
    { t: table, r: row }
  )
}

export async function getRow<T = unknown>(
  page: Page,
  table: string,
  key: string
): Promise<T | undefined> {
  await exposeReady(page)
  return page.evaluate(
    async ({ t, k }) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return await (window as any).__db__[t].get(k)
    },
    { t: table, k: key }
  )
}
