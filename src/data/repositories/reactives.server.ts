import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { StoredReactive } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import { subscribeAuthChange } from '../auth-events'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

// Stage 3 / 批次-3a — server-routed reactives KV.
//
// Envelope is KV-shaped (`{key, ...}` instead of providers' `{id, ...}`),
// and `data` carries the raw value blob — distinct from providers where
// `data` is the full row. Front-end re-wraps to {key, value: row.data}
// for IndexedDB so the StoredReactive shape stays unchanged.
//
// IndexedDB primary key is `key` (see schema in src/utils/db.ts), so cache
// reads/writes/observes use the key directly — no special composite logic
// on the client side.
interface ReactiveRow {
  key: string
  version: number
  updated_at: string
  deleted: boolean
  data: unknown
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<ReactiveRow>('reactives', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.reactives.delete(e.id)
        } else if (e.op === 'put' && e.row) {
          await db.reactives.put({ key: e.row.key, value: e.row.data })
        }
        if (e.rev > lastVersion) lastVersion = e.rev
      } catch (err) {
        console.warn('[reactives.server] realtime apply failed', err)
      }
    })()
  })
}

// Bug 1/2/3/4 fix — see providers.server.ts for the rationale.
subscribeAuthChange(() => {
  if (realtimeUnsubscribe) {
    try { realtimeUnsubscribe() } catch { /* ignore */ }
    realtimeUnsubscribe = null
  }
  inflight = null
  lastVersion = 0
})

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.reactives.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<ReactiveRow[]>('/api/v1/reactives', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.reactives, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.reactives.delete(row.key)
          } else {
            await db.reactives.put({ key: row.key, value: row.data })
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

function encodeKey(key: string): string {
  // Reactive keys are application-defined identifiers like '#user-data'.
  // `#` would otherwise be parsed as a URL fragment by browsers / proxies,
  // so we always percent-encode regardless of the actual character set.
  return encodeURIComponent(key)
}

export const serverReactivesRepository: Repository<StoredReactive, string> = (() => {
  const cache = createDexieRepository<StoredReactive, string>(
    () => db.reactives as Table<StoredReactive, string>
  )

  async function putOne(value: StoredReactive): Promise<string> {
    ensureRealtimeSubscription()
    // Server stores only the `value` blob; envelope wraps `{key, value, ...}`.
    const row = await http.put<ReactiveRow>(
      `/api/v1/reactives/${encodeKey(value.key)}`,
      value.value
    )
    await db.reactives.put({ key: row.key, value: row.data })
    if (row.version > lastVersion) lastVersion = row.version
    return value.key
  }

  async function deleteOne(key: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<ReactiveRow>(`/api/v1/reactives/${encodeKey(key)}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.reactives.delete(key)
  }

  return {
    table: cache.table,

    async get(key) {
      // persistent-reactive subscribes via `useLiveQuery(() => get(key))` —
      // not `observeOne` — so realtime activation must hang off `get` too,
      // otherwise cross-tab updates never write to the cache and the
      // liveQuery never re-fires for this key.
      ensureRealtimeSubscription()
      const hit = await cache.get(key)
      if (hit) return hit
      try {
        const row = await http.get<ReactiveRow>(`/api/v1/reactives/${encodeKey(key)}`)
        if (row.deleted) return undefined
        const stored: StoredReactive = { key: row.key, value: row.data }
        await db.reactives.put(stored)
        if (row.version > lastVersion) lastVersion = row.version
        return stored
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
    async update(key, changes) {
      const current = await cache.get(key)
      if (!current) return 0
      await putOne(shallowMerge(current, changes))
      return 1
    },
    delete: deleteOne,
    async bulkDelete(keys) {
      for (const k of keys) await deleteOne(k)
    },
    async deleteWhere(spec: QuerySpec<StoredReactive>) {
      await pull()
      const keys = await cache.findKeys(spec)
      for (const k of keys) await deleteOne(k)
      return keys.length
    },
    async modifyWhere(spec: QuerySpec<StoredReactive>, changes) {
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

    observeList: ((options) => {
      ensureRealtimeSubscription()
      return cache.observeList(options)
    }) as typeof cache.observeList,
    observeFind: ((spec, options) => {
      ensureRealtimeSubscription()
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((key, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(key, options)
    }) as typeof cache.observeOne
  }
})()
