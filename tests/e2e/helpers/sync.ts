// Cross-tab / cross-context sync waits. Phase-2+ specs lean on these heavily
// — `expectRowSync` is the verb that maps the plan's "B 1.5s 内能看到" to
// runnable assertion. Polls dumpTable; when pages are on different ports
// (different profiles) they share IndexedDB only via the backend round-trip.
import { expect, type Page } from '@playwright/test'
import { dumpTable } from './db'

export interface ExpectRowSyncOpts {
  withinMs?: number
  pollMs?: number
}

interface Row { id: string; version?: number }

function findById(rows: unknown[], id: string): Row | undefined {
  return (rows as Row[]).find(r => r.id === id)
}

export async function expectRowSync(
  pageA: Page,
  pageB: Page,
  table: string,
  id: string,
  opts: ExpectRowSyncOpts = {}
): Promise<void> {
  const within = opts.withinMs ?? 1_500
  const poll = opts.pollMs ?? 50
  const start = Date.now()
  let lastA: Row | undefined
  let lastB: Row | undefined
  while (Date.now() - start < within) {
    const [a, b] = await Promise.all([
      dumpTable(pageA, table),
      dumpTable(pageB, table)
    ])
    lastA = findById(a, id)
    lastB = findById(b, id)
    if (lastA && lastB && JSON.stringify(lastA) === JSON.stringify(lastB)) return
    await new Promise(resolve => setTimeout(resolve, poll))
  }
  expect(
    lastB,
    `row ${table}/${id} did not converge within ${within}ms (A=${JSON.stringify(lastA)} B=${JSON.stringify(lastB)})`
  ).toEqual(lastA)
}

// KV-shaped tables (reactives) use `key` as the identity slot, not `id`.
// Generic helper that takes the field name so we can reuse the converge-poll
// loop without duplicating the diagnostic message format.
export async function expectKvRowSync(
  pageA: Page,
  pageB: Page,
  table: string,
  field: string,
  value: string,
  opts: ExpectRowSyncOpts = {}
): Promise<void> {
  const within = opts.withinMs ?? 1_500
  const poll = opts.pollMs ?? 50
  const start = Date.now()
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let lastA: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let lastB: any
  while (Date.now() - start < within) {
    const [a, b] = await Promise.all([
      dumpTable(pageA, table),
      dumpTable(pageB, table)
    ])
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    lastA = (a as any[]).find(r => r[field] === value)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    lastB = (b as any[]).find(r => r[field] === value)
    if (lastA && lastB && JSON.stringify(lastA) === JSON.stringify(lastB)) return
    await new Promise(resolve => setTimeout(resolve, poll))
  }
  expect(
    lastB,
    `row ${table}/${field}=${value} did not converge within ${within}ms (A=${JSON.stringify(lastA)} B=${JSON.stringify(lastB)})`
  ).toEqual(lastA)
}

export async function waitForVersion(
  page: Page,
  table: string,
  id: string,
  minVersion: number,
  timeoutMs = 1_500
): Promise<Row> {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    const rows = await dumpTable(page, table)
    const row = findById(rows, id)
    if (row && (row.version ?? 0) >= minVersion) return row
    await new Promise(resolve => setTimeout(resolve, 50))
  }
  throw new Error(
    `waitForVersion(${table}/${id} >= ${minVersion}) timed out after ${timeoutMs}ms`
  )
}
