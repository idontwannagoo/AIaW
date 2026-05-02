import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { Folder, Workspace } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'

// Stage 4 / 批次-4a — server-routed workspaces. id-PK envelope mirrors
// providers.server.ts / assistants.server.ts; the only differences are the
// table name, the WorkspaceOrFolder generic, and `delete()` passing
// `?cascade=true` to the server (which is a no-op at 4a but already on the
// wire so 4b/4c/4d/4e can grow real cascade behind it without revving the
// client).
type WorkspaceOrFolder = Workspace | Folder

interface WorkspaceRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: WorkspaceOrFolder | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<WorkspaceRow>('workspaces', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.workspaces.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          await db.workspaces.put(e.row.data)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
      } catch (err) {
        console.warn('[workspaces.server] realtime apply failed', err)
      }
    })()
  })
}

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.workspaces.count()
      if (cached === 0) lastVersion = 0

      const rows = await http.get<WorkspaceRow[]>('/api/v1/workspaces', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.workspaces, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.workspaces.delete(row.id)
          } else if (row.data) {
            await db.workspaces.put(row.data)
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

export const serverWorkspacesRepository: Repository<WorkspaceOrFolder, string> = (() => {
  const cache = createDexieRepository<WorkspaceOrFolder, string>(
    () => db.workspaces as Table<WorkspaceOrFolder, string>
  )

  async function putOne(value: WorkspaceOrFolder): Promise<string> {
    ensureRealtimeSubscription()
    const row = await http.put<WorkspaceRow>(`/api/v1/workspaces/${value.id}`, value)
    if (row.data) await db.workspaces.put(row.data)
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      // cascade=true is forward-compat at 4a (server-side no-op for now;
      // see routers/workspaces.py module docstring). Always sending it
      // means the wire shape stops changing once 4b lands real cascade.
      await http.delete<WorkspaceRow>(`/api/v1/workspaces/${id}`, {
        query: { cascade: true }
      })
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.workspaces.delete(id)
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<WorkspaceRow>(`/api/v1/workspaces/${id}`)
        if (row.deleted || !row.data) return undefined
        await db.workspaces.put(row.data)
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
    async deleteWhere(spec: QuerySpec<WorkspaceOrFolder>) {
      await pull()
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<WorkspaceOrFolder>, changes) {
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
