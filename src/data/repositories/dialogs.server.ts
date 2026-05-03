import type { Table } from 'dexie'
import { db } from 'src/utils/db'
import { RealtimeTransport } from 'src/utils/config'
import type { Dialog } from 'src/utils/types'
import { http, HttpError } from '../http'
import { realtime } from '../realtime'
import { subscribeAuthChange } from '../auth-events'
import type { QuerySpec, Repository } from '../types'
import { createDexieRepository } from './dexie'
import { createScopedPull, extractScopeId } from './scoped-pull'

// Stage 4 / 批次-4b — server-routed dialogs. id-PK envelope mirrors
// workspaces.server.ts; the only differences are the table name, the
// Dialog generic, and that the server validates `data.workspaceId` exists
// before accepting the upsert (returns 409 if not). The frontend doesn't
// need to do anything special for that — Repository.put() / add() forward
// errors to the caller, who decides whether to retry or surface.
//
// Cascade-on-workspace-delete is handled server-side: routers/workspaces.py
// publishes a stream of `delete` events for every dialog under the
// workspace before the workspace's own tombstone event. Live tabs receive
// each event and reapply the same idempotent delete path the frontend uses
// for explicit dialogs.delete() calls.
//
// Stage 4 / 硬前置 3 — scoped pull. `observeFind / find / findFirst /
// findKeys / count` inspect `spec.where.workspaceId`; if a single scalar
// scopeId is present, the read goes through `scopedPull` which fetches
// `?workspaceId=X&since=<scope-cursor>` and bumps that scope's cursor in
// isolation from the full-table cursor. Spec-less or multi-value reads
// fall back to the full-table `pull()` for compatibility (some stores
// still call `repos.dialogs.list()` to enumerate everything).
interface DialogRow {
  id: string
  version: number
  updated_at: string
  deleted: boolean
  data: Dialog | null
}

let lastVersion = 0
let inflight: Promise<void> | null = null
let realtimeUnsubscribe: (() => void) | null = null

const scopedPull = createScopedPull<Dialog>({
  tableName: 'dialogs',
  scopeField: 'workspaceId',
  fetchFn: async (workspaceId, since) => {
    const rows = await http.get<DialogRow[]>('/api/v1/dialogs', {
      query: { workspaceId, since }
    })
    if (!rows.length) return { maxVersion: 0 }
    let maxVersion = 0
    await db.transaction('rw', db.dialogs, async () => {
      for (const row of rows) {
        if (row.deleted) {
          await db.dialogs.delete(row.id)
        } else if (row.data) {
          await db.dialogs.put(row.data)
        }
        if (row.version > maxVersion) maxVersion = row.version
        // Also advance the full-table cursor when scoped pulls observe a
        // higher version than full-table pulls have seen — the global
        // cursor is monotonic across all scopes (versions come from the
        // shared `global_change_seq`), so refusing to advance it would
        // force an unnecessary re-pull of rows we've already cached.
        if (row.version > lastVersion) lastVersion = row.version
      }
    })
    return { maxVersion }
  }
})

function ensureRealtimeSubscription(): void {
  if (!RealtimeTransport) return
  if (realtimeUnsubscribe) return
  realtimeUnsubscribe = realtime.subscribe<DialogRow>('dialogs', (e) => {
    void (async () => {
      try {
        if (e.op === 'delete') {
          await db.dialogs.delete(e.id)
        } else if (e.op === 'put' && e.row && e.row.data) {
          await db.dialogs.put(e.row.data)
        }
        if (e.rev > lastVersion) lastVersion = e.rev
        // Forward to scopedPull AFTER cache write so scope cursors only
        // advance for events that have actually been applied locally.
        scopedPull.applyEvent(e)
      } catch (err) {
        console.warn('[dialogs.server] realtime apply failed', err)
      }
    })()
  })
}

// Bug 1/2/3/4 fix — see providers.server.ts. dialogs additionally has
// scoped-pull state (per-workspaceId cursors) that must be reset, otherwise
// the next account's first observeFind({where:{workspaceId}}) treats the
// previous user's scope cursor as cached and skips the pull.
subscribeAuthChange(() => {
  if (realtimeUnsubscribe) {
    try { realtimeUnsubscribe() } catch { /* ignore */ }
    realtimeUnsubscribe = null
  }
  inflight = null
  lastVersion = 0
  scopedPull.reset()
})

async function pull(): Promise<void> {
  if (inflight) return inflight
  inflight = (async () => {
    try {
      const cached = await db.dialogs.count()
      if (cached === 0) {
        lastVersion = 0
        scopedPull.reset()
      }

      const rows = await http.get<DialogRow[]>('/api/v1/dialogs', {
        query: { since: lastVersion }
      })
      if (!rows.length) return
      await db.transaction('rw', db.dialogs, async () => {
        for (const row of rows) {
          if (row.deleted) {
            await db.dialogs.delete(row.id)
          } else if (row.data) {
            await db.dialogs.put(row.data)
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

async function pullForSpec(spec: QuerySpec<Dialog> | undefined): Promise<void> {
  const scopeId = extractScopeId(
    spec?.where as Record<string, unknown> | undefined,
    'workspaceId'
  )
  if (scopeId) {
    await scopedPull.pullScope(scopeId)
    return
  }
  await pull()
}

export const serverDialogsRepository: Repository<Dialog, string> = (() => {
  const cache = createDexieRepository<Dialog, string>(
    () => db.dialogs as Table<Dialog, string>
  )

  async function putOne(value: Dialog): Promise<string> {
    ensureRealtimeSubscription()
    const row = await http.put<DialogRow>(`/api/v1/dialogs/${value.id}`, value)
    if (row.data) await db.dialogs.put(row.data)
    if (row.version > lastVersion) lastVersion = row.version
    return value.id
  }

  async function deleteOne(id: string): Promise<void> {
    ensureRealtimeSubscription()
    try {
      await http.delete<DialogRow>(`/api/v1/dialogs/${id}`)
    } catch (e) {
      if (!(e instanceof HttpError) || e.status !== 404) throw e
    }
    await db.dialogs.delete(id)
  }

  function shallowMerge<T>(base: T, changes: Partial<T> | Record<string, unknown>): T {
    return { ...(base as object), ...(changes as object) } as T
  }

  return {
    table: cache.table,

    async get(id) {
      ensureRealtimeSubscription()
      const hit = await cache.get(id)
      if (hit) return hit
      try {
        const row = await http.get<DialogRow>(`/api/v1/dialogs/${id}`)
        if (row.deleted || !row.data) return undefined
        await db.dialogs.put(row.data)
        if (row.version > lastVersion) lastVersion = row.version
        return row.data
      } catch (e) {
        if (e instanceof HttpError && e.status === 404) return undefined
        throw e
      }
    },
    async list() { await pull(); return cache.list() },
    async find(spec) { await pullForSpec(spec); return cache.find(spec) },
    async findFirst(spec) { await pullForSpec(spec); return cache.findFirst(spec) },
    async findKeys(spec) { await pullForSpec(spec); return cache.findKeys(spec) },
    async count(spec) { await pullForSpec(spec); return cache.count(spec) },

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
    async deleteWhere(spec: QuerySpec<Dialog>) {
      await pullForSpec(spec)
      const ids = await cache.findKeys(spec)
      for (const id of ids) await deleteOne(id)
      return ids.length
    },
    async modifyWhere(spec: QuerySpec<Dialog>, changes) {
      await pullForSpec(spec)
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
      // Kick off a scope-aware pull on mount so the first render isn't
      // empty for scopes we haven't seen yet. Errors are swallowed; the
      // observe ref will repopulate as the cache catches up via realtime.
      const initial = typeof spec === 'function' ? spec() : spec
      void pullForSpec(initial).catch((err) => {
        console.warn('[dialogs.server] observeFind initial pull failed', err)
      })
      return cache.observeFind(spec, options)
    }) as typeof cache.observeFind,
    observeOne: ((id, options) => {
      ensureRealtimeSubscription()
      return cache.observeOne(id, options)
    }) as typeof cache.observeOne
  }
})()
