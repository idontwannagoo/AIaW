import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { InstalledPlugin } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import { subscribeAuthChange } from '../auth-events'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

// Stage 3 / 批次-3b — server-routed installed_plugins.
//
// KV-shaped wire envelope (composite (user_id, key) PK, mirrors reactives),
// but the unwrap differs: reactives' `data` is the value blob, here `data`
// IS the full InstalledPlugin row. So `db.installedPluginsV2.put(row.data)`
// — same shape as providers/assistants on the cache side. Matches the
// per-table envelope allowance noted in the cloud-sync-migration plan.
//
// IndexedDB primary key is `key` (see schema in src/utils/db.ts), so the
// dexie cache uses the key directly — no special composite logic on the
// client side.
interface InstalledPluginRow {
  key: string
  version: number
  updated_at: string
  deleted: boolean
  data: InstalledPlugin | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<InstalledPluginRow>(
    'installed_plugins',
    (e) => {
      void (async () => {
        try {
          if (e.op === 'delete') {
            await db.installedPluginsV2.delete(e.id)
          } else if (e.op === 'put' && e.row && e.row.data) {
            await db.installedPluginsV2.put(e.row.data)
          }
          if (e.rev > lastVersion) lastVersion = e.rev
        } catch (err) {
          console.warn('[installed-plugins.server] realtime apply failed', err)
        }
      })()
    }
  )
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
      const cached = await db.installedPluginsV2.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<InstalledPluginRow[]>('/api/v1/installed-plugins', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      // No db.transaction() here — Dexie's transaction() overload does
      // KeyPath inference on the table type, which recurses infinitely
      // through InstalledPlugin's manifest → JSONSchema7 → oneOf/anyOf/allOf.
      // Per-row puts give the same atomicity for our use (each PUT is its own
      // version anyway) without dragging the path-key inference machinery in.
      for (const row of rows) {
        if (row.deleted) {
          await db.installedPluginsV2.delete(row.key)
        } else if (row.data) {
          await db.installedPluginsV2.put(row.data)
        }
        if (row.version > lastVersion) lastVersion = row.version
      }
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
  // Plugin keys can contain `:` (namespaced like 'lobe:foo'). The server
  // route is `{key:path}` so colon survives, but we still encodeURIComponent
  // for any oddball character (slashes, hash, etc).
  return encodeURIComponent(key)
}

export const serverInstalledPluginsRepository: Repository<InstalledPlugin, string> = (() => {
  const cache = createDexieRepository<InstalledPlugin, string>(
    () => db.installedPluginsV2 as Table<InstalledPlugin, string>
  )

  async function putOne(value: InstalledPlugin): Promise<string> {
    ensureRealtimeSubscription()
    // Bug 6 perf fix — local-first cache write; see messages.server.ts.
    await db.installedPluginsV2.put(value)
    const row = await http.put<InstalledPluginRow>(
      `/api/v1/installed-plugins/${encodeKey(value.key)}`,
      value
    )
    if (row.data) await db.installedPluginsV2.put(row.data)
    if (row.version > lastVersion) lastVersion = row.version
    return value.key
  }

  async function deleteOne(key: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<InstalledPluginRow>(
        `/api/v1/installed-plugins/${encodeKey(key)}`
      )
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.installedPluginsV2.delete(key)
  }

  return {
    table: cache.table,

    async get(key) {
      ensureRealtimeSubscription()
      const hit = await cache.get(key)
      if (hit) return hit
      try {
        const row = await http.get<InstalledPluginRow>(
          `/api/v1/installed-plugins/${encodeKey(key)}`
        )
        if (row.deleted || !row.data) return undefined
        await db.installedPluginsV2.put(row.data)
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
    async deleteWhere(spec: QuerySpec<InstalledPlugin>) {
      await pull()
      const keys = await cache.findKeys(spec)
      for (const k of keys) await deleteOne(k)
      return keys.length
    },
    async modifyWhere(spec: QuerySpec<InstalledPlugin>, changes) {
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
