import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { Assistant } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import { subscribeAuthChange } from '../auth-events'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

// Stage 3 / 批次-3b — server-routed assistants (id-PK envelope, mirrors
// providers.server.ts byte-for-byte; the differences are limited to the
// table name and the Repository<T> generic).
interface AssistantRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Assistant | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<AssistantRow>('assistants', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.assistants.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          await db.assistants.put(e.row.data)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
      } catch (err) {
        console.warn('[assistants.server] realtime apply failed', err)
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
      const cached = await db.assistants.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<AssistantRow[]>('/api/v1/assistants', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.assistants, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.assistants.delete(row.id)
          } else if (row.data) {
            await db.assistants.put(row.data)
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

export const serverAssistantsRepository: Repository<Assistant, string> = (() => {
  const cache = createDexieRepository<Assistant, string>(
    () => db.assistants as Table<Assistant, string>
  )

  async function putOne(value: Assistant): Promise<string> {
    ensureRealtimeSubscription()
    const row = await http.put<AssistantRow>(`/api/v1/assistants/${value.id}`, value)
    if (row.data) await db.assistants.put(row.data)
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<AssistantRow>(`/api/v1/assistants/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.assistants.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<AssistantRow>(`/api/v1/assistants/${id}`)
        if (row.deleted || !row.data) return undefined
        await db.assistants.put(row.data)
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
    async deleteWhere(spec: QuerySpec<Assistant>) {
      await pull()
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<Assistant>, changes) {
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
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
