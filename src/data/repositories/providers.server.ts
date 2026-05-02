import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { CustomProvider } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

interface ProviderRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: CustomProvider | null
}

// Module state — single-tab assumption for Stage 1. Stage 2's WS pub/sub
// will replace polling and reset this on reconnect.
let lastVersion = 0
let inflight: Promise<void> | null = null

// Stage 2 / Step 4-5: server-driven cache writes. Subscribed once per page on
// first observe call; events from the active transport mirror into
// db.providers, and liveQuery turns the IDB write into a UI refresh. Empty
// `RealtimeTransport` (providers-rest profile) keeps the cache purely
// pull-based.
let realtimeUnsubscribe: (() => void) | null = null

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  // Server emits the full ProviderRow envelope as `row`, mirroring what
  // `GET /api/v1/providers` returns. Unwrap to `row.data` so the cache stays
  // in CustomProvider shape — same contract as `pull()` above.
  realtimeUnsubscribe = realtime.subscribe<ProviderRow>('providers', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.providers.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          await db.providers.put(e.row.data)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
      } catch (err) {
        console.warn('[providers.server] realtime apply failed', err)
      }
    })()
  })
}

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      // Cache emptied externally (Console clear, IndexedDB wipe) → re-bootstrap
      // from version 0 instead of returning empty silently.
      const cached = await db.providers.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<ProviderRow[]>('/api/v1/providers', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.providers, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.providers.delete(row.id)
          } else if (row.data) {
            await db.providers.put(row.data)
          }
          if (row.version > lastVersion) lastVersion = row.version
        }
      })
    } finally {
      inflight = null
    }
  })()
  return inflight
}

function shallowMerge<T>(base: T, changes: Partial<T> | Record<string, unknown>): T {
  return { ...(base as object), ...(changes as object) } as T
}

export const serverProvidersRepository: Repository<CustomProvider, string> = (() => {
  // dexie-cloud-addon widens db.providers to DexieCloudTable; the cache
  // helper only needs the plain Table surface, so cast to drop the addon
  // fields. Mirrors the pattern used in repositories/index.ts.
  const cache = createDexieRepository<CustomProvider, string>(
    () => db.providers as unknown as Table<CustomProvider, string>
  )

  async function putOne(value: CustomProvider): Promise<string> {
    const row = await http.put<ProviderRow>(`/api/v1/providers/${value.id}`, value)
    if (row.data) await db.providers.put(row.data)
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    try {
      await http.delete<ProviderRow>(`/api/v1/providers/${id}`)
    } catch (e) {
      // 404 = already gone server-side; tolerate so caller's mental model
      // stays "after delete it's gone".
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.providers.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<ProviderRow>(`/api/v1/providers/${id}`)
        if (row.deleted || !row.data) return undefined
        await db.providers.put(row.data)
        if (row.version > lastVersion) lastVersion = row.version
        return row.data
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return undefined
        throw e
      }
    },
    async list() { await pull(); return cache.list() },
    async find(spec) { await pull(); return cache.find(spec) },
    async findFirst(spec) { await pull(); return cache.findFirst(spec) },
    async findKeys(spec) { await pull(); return cache.findKeys(spec) },
    async count(spec) { await pull(); return cache.count(spec) },

    add: putOne,
    put: putOne,
    async bulkPut(values) {
      let last = ''
      for (const v of values) last = await putOne(v)
      return last
    },
    async update(id, changes) {
      const current = await cache.get(id)
      if (!current) return 0
      await putOne(shallowMerge(current, changes))
      return 1
    },
    delete: deleteOne,
    async bulkDelete(ids) {
      for (const id of ids) await deleteOne(id)
    },
    async deleteWhere(spec: QuerySpec<CustomProvider>) {
      await pull()
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<CustomProvider>, changes) {
      await pull()
      const rows = await cache.find(spec)
      for (const r of rows) await putOne(shallowMerge(r, changes))
      return rows.length
    },
    async modifyAll(predicate, changes) {
      await pull()
      const all = await cache.list()
      const matches = all.filter(predicate)
      for (const r of matches) await putOne(shallowMerge(r, changes))
      return matches.length
    },

    // Observers ride on Dexie's liveQuery — server writes mirror to db.providers
    // here, plus the WS subscription started below pushes remote events into
    // the same cache, so subscribers see live updates without extra wiring.
    observeList: ((options) => {
      ensureRealtimeSubscription()
      return cache.observeList(options)
    }) as typeof cache.observeList,
    observeFind: ((spec, options) => {
      ensureRealtimeSubscription()
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
